<script src="https://apis.google.com/js/platform.js" async defer></script>
<meta name="google-signin-client_id" content="961602928472-e1l8cg4tprka7rhivpu1rhd217020g79.apps.googleusercontent.com"></meta>
<div class="g-signin2" data-onsuccess="onSignIn"></div>

function onSignIn(googleUser) {
  var profile = googleUser.getBasicProfile();
  console.log('ID: ' + profile.getId()); // Do not send to your backend! Use an ID token instead.
  console.log('Name: ' + profile.getName());
  console.log('Image URL: ' + profile.getImageUrl());
  console.log('Email: ' + profile.getEmail()); // This is null if the 'email' scope is not present.
}

<a href="#" onclick="signOut();">Sign out</a>
<script>
  function signOut() {
    var auth2 = gapi.auth2.getAuthInstance();
    auth2.signOut().then(function () {
      console.log('User signed out.');
    });
  }
</script>